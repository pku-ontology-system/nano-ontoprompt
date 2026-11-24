import {createContext,useContext,type ReactNode} from 'react';
import type {State,Resource} from './model';
export type AppContext={state:State;update:(fn:(s:State)=>State)=>boolean;path:string;pathname:string;params:URLSearchParams;go:(path:string)=>void;query:(values:Record<string,string>)=>void;source:(path:string)=>string;toast:(text:string)=>void;unavailable:(name:string)=>void;dialog:(title:string,body:ReactNode)=>void;close:()=>void;openResource:(r:Resource)=>void;favorite:(id:string)=>void;time:(date:string)=>string};
export const Context=createContext<AppContext>(null!);
export const useApp=()=>useContext(Context);
export function Link({to,children,className='',title,source=false,...props}:{to:string;children:ReactNode;className?:string;title?:string;source?:boolean;[key:string]:unknown}){const a=useApp();return <a {...props} href={'#'+(source?a.source(to):to)} className={className} title={title}>{children}</a>;}
